"""MediSlot Milestone 5: booking engine, cancellation and refunds.

Routes (all behind the JWT authorizer; role is re-checked here):
    POST /appointments                         PATIENT  book a slot (Idempotency-Key required)
    GET  /appointments                         any      list the caller's appointments
    GET  /appointments/{appointment_id}        any      detail, scoped to the caller
    DELETE /appointments/{appointment_id}      PATIENT  cancel, refund tier from SSM windows
    GET  /patient/refunds                      PATIENT  refund ledger
    GET  /doctor/appointments                  DOCTOR   list the doctor's appointments
    POST /doctor/slots/{slot_id}/cancel        DOCTOR   cancel a slot, full refund for everyone

Design (see docs/concurrency-design-note.md):
  * The seat counter lives on a DynamoDB slot record guarded by a `version`
    attribute. A seat is taken with a conditional UpdateItem
    (version = :seen AND booked_count < capacity). Losers re-read and retry
    with jittered exponential backoff; retries are published as the
    BookingConflictRetries metric.
  * RDS is the system of record for appointments and the refund ledger.
  * A DynamoDB idempotency store replays the original appointment for a key.
"""

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from common import body as read_body, config, correlation, dbconn, response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

IST = ZoneInfo("Asia/Kolkata")
ENV = os.environ.get("ENVIRONMENT", "dev")
KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")

BACKOFF_BASE = float(os.environ.get("BOOKING_BACKOFF_BASE", "0.02"))
BACKOFF_CAP = float(os.environ.get("BOOKING_BACKOFF_CAP", "0.25"))
MIN_ATTEMPTS = int(os.environ.get("BOOKING_MIN_ATTEMPTS", "6"))
IDEMPOTENCY_TTL = 24 * 3600
IN_PROGRESS_STALE = 60

# Frozen Rule Set: >48h 100%, 24-48h 75%, 6-24h 50%, <6h 0%. All values live in SSM.
DEFAULT_REFUND = {
    "t1_hours": 48, "t2_hours": 24, "t3_hours": 6,
    "t1_percent": 100, "t2_percent": 75, "t3_percent": 50, "t4_percent": 0,
}
DEFAULT_LIMITS = {"max_upcoming_per_doctor": 3}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class VersionConflict(Exception):
    """Conditional write on the slot record lost the race."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ist(value):
    return value.replace(tzinfo=timezone.utc).astimezone(IST).isoformat()


def money(value):
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


# --------------------------------------------------------------------------- #
# Refund tiers (pure, unit tested at every boundary instant)
# --------------------------------------------------------------------------- #

def refund_tier(slot_start, cancelled_at, cfg=None):
    """Return (tier, percent) for a patient cancellation.

    T1: strictly MORE than t1_hours before the start       (default 100 %)
    T2: t2_hours up to and including t1_hours               (default  75 %)
    T3: t3_hours up to just under t2_hours                  (default  50 %)
    T4: less than t3_hours                                  (default   0 %)
    So exactly 48 h is T2, exactly 24 h is T2, exactly 6 h is T3."""
    cfg = cfg or DEFAULT_REFUND
    seconds = (slot_start - cancelled_at).total_seconds()
    if seconds > cfg["t1_hours"] * 3600:
        return "T1", int(cfg["t1_percent"])
    if seconds >= cfg["t2_hours"] * 3600:
        return "T2", int(cfg["t2_percent"])
    if seconds >= cfg["t3_hours"] * 3600:
        return "T3", int(cfg["t3_percent"])
    return "T4", int(cfg["t4_percent"])


def refund_amount(fee, percent):
    return (Decimal(str(fee)) * Decimal(percent) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


# --------------------------------------------------------------------------- #
# Configuration from SSM (60 s cache so tuning needs no redeploy)
# --------------------------------------------------------------------------- #

_ssm_cache = {}


def ssm_int(name, default):
    hit = _ssm_cache.get(name)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        from common import ssm
        value = int(ssm.get_parameter(Name=f"/medislot/{ENV}/{name}")["Parameter"]["Value"])
    except Exception:
        value = default
    _ssm_cache[name] = (time.time(), value)
    return value


def refund_config():
    return {
        key: ssm_int("refund/" + key.replace("_", "-"), default)
        for key, default in DEFAULT_REFUND.items()
    }


def limit_config():
    return {
        "max_upcoming_per_doctor": ssm_int(
            "booking/max-upcoming-per-doctor", DEFAULT_LIMITS["max_upcoming_per_doctor"]
        ),
    }


def enforce_limits(upcoming, slot, cfg):
    """`upcoming`: the patient's future CONFIRMED appointments (slot_id, doctor_id).
    Frozen rules: one appointment per slot, at most N upcoming with the same doctor."""
    if any(int(a["slot_id"]) == int(slot["slot_id"]) for a in upcoming):
        raise ApiError(409, "ALREADY_BOOKED", "You already hold a seat in this slot.")
    same_doctor = sum(1 for a in upcoming if int(a["doctor_id"]) == int(slot["doctor_id"]))
    if same_doctor >= cfg["max_upcoming_per_doctor"]:
        raise ApiError(
            422, "LIMIT_PER_DOCTOR",
            f"You can hold at most {cfg['max_upcoming_per_doctor']} upcoming appointments with the same doctor.",
        )


# --------------------------------------------------------------------------- #
# Metrics (CloudWatch Embedded Metric Format: no network call from the VPC)
# --------------------------------------------------------------------------- #

class Metrics:
    namespace = "medislot"

    def count(self, name, value=1, unit="Count", **dims):
        dims = {"Environment": ENV, **dims}
        print(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": self.namespace,
                    "Dimensions": [list(dims)],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }],
            },
            **dims,
            name: value,
        }))


# --------------------------------------------------------------------------- #
# EventBridge
# --------------------------------------------------------------------------- #

class Events:
    def __init__(self):
        self._client = None
        self.cid = None  # correlation id of the request being served

    def publish(self, detail_type, detail):
        if self.cid:
            detail = {**detail, "correlationId": self.cid}
        try:
            if self._client is None:
                import boto3
                self._client = boto3.client("events")
            out = self._client.put_events(Entries=[{
                "Source": "medislot.appointments",
                "DetailType": detail_type,
                "Detail": json.dumps(detail, default=str),
                "EventBusName": "default",
            }])
            if out.get("FailedEntryCount"):
                logger.error("EventBridge rejected %s: %s", detail_type, out.get("Entries"))
        except Exception:
            # The booking is already committed in RDS; never fail the caller.
            logger.exception("Could not publish %s", detail_type)


# --------------------------------------------------------------------------- #
# DynamoDB: slot record with optimistic concurrency (version attribute)
# --------------------------------------------------------------------------- #

def _ddb_table(suffix):
    from common import table
    return table(f"MediSlot-{ENV}-{suffix}")


def _plain(item):
    return {k: (int(v) if isinstance(v, Decimal) else v) for k, v in item.items()}


def _conditional_failed(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class SlotStore:
    def __init__(self):
        self._t = None

    @property
    def t(self):
        if self._t is None:
            self._t = _ddb_table("Slots")
        return self._t

    def get(self, slot_id):
        item = self.t.get_item(Key={"slot_id": str(slot_id)}, ConsistentRead=True).get("Item")
        return _plain(item) if item else None

    def create(self, slot):
        """Lazily create the slot record from the RDS slot row (idempotent)."""
        start = slot["slot_start"].isoformat()
        try:
            self.t.put_item(
                Item={
                    "slot_id": str(slot["slot_id"]),
                    "doctor_id": int(slot["doctor_id"]),
                    "doctor_date": f"{slot['doctor_id']}#{start[:10]}",
                    "start_at": start,
                    "capacity": int(slot["capacity"]),
                    "booked_count": 0,
                    "version": 0,
                    "status": "OPEN",
                },
                ConditionExpression="attribute_not_exists(slot_id)",
            )
        except Exception as exc:
            if not _conditional_failed(exc):
                raise

    def reserve(self, slot_id, seen_version):
        try:
            out = self.t.update_item(
                Key={"slot_id": str(slot_id)},
                UpdateExpression="SET booked_count = booked_count + :one, version = version + :one",
                ConditionExpression="version = :v AND booked_count < #cap AND #s = :open",
                ExpressionAttributeNames={"#s": "status", "#cap": "capacity"},
                ExpressionAttributeValues={":one": 1, ":v": seen_version, ":open": "OPEN"},
                ReturnValues="ALL_NEW",
            )
            return _plain(out["Attributes"])
        except Exception as exc:
            if _conditional_failed(exc):
                raise VersionConflict()
            raise

    def release(self, slot_id, seen_version):
        try:
            out = self.t.update_item(
                Key={"slot_id": str(slot_id)},
                UpdateExpression="SET booked_count = booked_count - :one, version = version + :one",
                ConditionExpression="version = :v AND booked_count > :zero",
                ExpressionAttributeValues={":one": 1, ":v": seen_version, ":zero": 0},
                ReturnValues="ALL_NEW",
            )
            return _plain(out["Attributes"])
        except Exception as exc:
            if _conditional_failed(exc):
                raise VersionConflict()
            raise

    def close(self, slot_id):
        """Terminal: the doctor cancelled the slot. Bumps the version so any
        in-flight reservation loses its conditional write."""
        self.t.update_item(
            Key={"slot_id": str(slot_id)},
            UpdateExpression="SET #s = :closed, booked_count = :zero ADD version :one",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":closed": "CLOSED", ":zero": 0, ":one": 1},
        )


# --------------------------------------------------------------------------- #
# DynamoDB: idempotency store
# --------------------------------------------------------------------------- #

class IdempotencyStore:
    def __init__(self):
        self._t = None

    @property
    def t(self):
        if self._t is None:
            self._t = _ddb_table("Idempotency")
        return self._t

    def begin(self, key, request_hash):
        """None when this call owns the key; otherwise the existing record."""
        now = int(time.time())
        try:
            self.t.put_item(
                Item={
                    "idempotency_key": key, "state": "IN_PROGRESS",
                    "request_hash": request_hash, "locked_at": now,
                    "expires_at": now + IDEMPOTENCY_TTL,
                },
                ConditionExpression=(
                    "attribute_not_exists(idempotency_key) OR "
                    "(#st = :ip AND locked_at < :stale)"
                ),
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={":ip": "IN_PROGRESS", ":stale": now - IN_PROGRESS_STALE},
            )
            return None
        except Exception as exc:
            if not _conditional_failed(exc):
                raise
        item = self.t.get_item(Key={"idempotency_key": key}, ConsistentRead=True).get("Item")
        return _plain(item) if item else self.begin(key, request_hash)

    def complete(self, key, result):
        self.t.update_item(
            Key={"idempotency_key": key},
            UpdateExpression="SET #st = :done, #r = :r",
            ExpressionAttributeNames={"#st": "state", "#r": "result"},
            ExpressionAttributeValues={":done": "COMPLETED", ":r": json.dumps(result, default=str)},
        )

    def abandon(self, key):
        self.t.delete_item(Key={"idempotency_key": key})


# --------------------------------------------------------------------------- #
# RDS repository (system of record)
# --------------------------------------------------------------------------- #

APPT_SELECT = (
    "SELECT a.appointment_id, a.slot_id, a.doctor_id, a.patient_id, a.consultation_type_id, "
    "a.slot_start, a.slot_end, a.fee, a.status, a.booked_at, a.cancelled_at, a.cancel_reason, "
    "c.name AS consultation_type, dp.full_name AS doctor_name, pt.full_name AS patient_name, "
    "r.amount AS refund_amount, r.refund_percent, r.tier AS refund_tier "
    "FROM appointments a "
    "JOIN consultation_catalogue c ON c.consultation_type_id = a.consultation_type_id "
    "LEFT JOIN doctor_profiles dp ON dp.user_id = a.doctor_id "
    "LEFT JOIN patients pt ON pt.patient_id = a.patient_id "
    "LEFT JOIN refund_ledger r ON r.appointment_id = a.appointment_id "
)


class Repo:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def patient_id(self, user_id):
        self.cur.execute("SELECT patient_id FROM patients WHERE user_id = %s", (user_id,))
        row = self.cur.fetchone()
        return int(row["patient_id"]) if row else None

    def get_slot(self, slot_id):
        self.cur.execute(
            "SELECT s.slot_id, s.doctor_id, s.consultation_type_id, s.slot_start, s.slot_end, "
            "s.status, c.name AS consultation_type, COALESCE(f.capacity, c.capacity) AS capacity, f.fee, dp.full_name AS doctor_name "
            "FROM appointment_slots s "
            "JOIN consultation_catalogue c ON c.consultation_type_id = s.consultation_type_id "
            "LEFT JOIN doctor_consultation_fees f ON f.doctor_id = s.doctor_id "
            " AND f.consultation_type_id = s.consultation_type_id AND f.is_active = 1 "
            "LEFT JOIN doctor_profiles dp ON dp.user_id = s.doctor_id "
            "WHERE s.slot_id = %s",
            (slot_id,),
        )
        return self.cur.fetchone()

    def upcoming(self, patient_id, now):
        self.cur.execute(
            "SELECT slot_id, doctor_id, slot_start, slot_end FROM appointments "
            "WHERE patient_id = %s AND status = 'CONFIRMED' AND slot_start > %s",
            (patient_id, now),
        )
        return self.cur.fetchall()

    def insert_appointment(self, patient_id, slot, key):
        """Returns (appointment, created). A duplicate idempotency key returns the
        existing appointment with created=False."""
        import pymysql
        try:
            self.cur.execute(
                "INSERT INTO appointments (slot_id, doctor_id, patient_id, consultation_type_id, "
                "slot_start, slot_end, fee, status, active_slot_id, idempotency_key) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'CONFIRMED', %s, %s)",
                (slot["slot_id"], slot["doctor_id"], patient_id, slot["consultation_type_id"],
                 slot["slot_start"], slot["slot_end"], slot["fee"], slot["slot_id"], key),
            )
            return self.get_appointment(self.cur.lastrowid), True
        except pymysql.err.IntegrityError as exc:
            self.conn.rollback()
            text = str(exc)
            if "uq_appt_patient_idem" in text:
                self.cur.execute("SELECT appointment_id FROM appointments WHERE patient_id = %s AND idempotency_key = %s", (patient_id, key))
                return self.get_appointment(self.cur.fetchone()["appointment_id"]), False
            if "uq_appt_patient_active_slot" in text:
                raise ApiError(409, "ALREADY_BOOKED", "You already hold a seat in this slot.")
            raise

    def find_by_key(self, patient_id, key):
        self.cur.execute(
            "SELECT appointment_id FROM appointments WHERE patient_id = %s AND idempotency_key = %s",
            (patient_id, key),
        )
        row = self.cur.fetchone()
        return self.get_appointment(row["appointment_id"]) if row else None

    def set_slot_status(self, slot_id, new, old):
        self.cur.execute(
            "UPDATE appointment_slots SET status = %s WHERE slot_id = %s AND status = %s",
            (new, slot_id, old),
        )

    def get_appointment(self, appointment_id, patient_id=None, doctor_id=None, for_update=False):
        sql, params = APPT_SELECT + "WHERE a.appointment_id = %s ", [appointment_id]
        if patient_id is not None:
            sql, params = sql + "AND a.patient_id = %s ", params + [patient_id]
        if doctor_id is not None:
            sql, params = sql + "AND a.doctor_id = %s ", params + [doctor_id]
        self.cur.execute(sql + ("FOR UPDATE" if for_update else ""), params)
        return self.cur.fetchone()

    def list_appointments(self, patient_id=None, doctor_id=None):
        col, val = ("a.patient_id", patient_id) if patient_id is not None else ("a.doctor_id", doctor_id)
        self.cur.execute(APPT_SELECT + f"WHERE {col} = %s ORDER BY a.slot_start DESC LIMIT 200", (val,))
        return self.cur.fetchall()

    def cancel(self, appt, status, reason, tier, percent, amount):
        """Status change and refund ledger row in the caller's transaction."""
        self.cur.execute(
            "UPDATE appointments SET status = %s, active_slot_id = NULL, "
            "cancelled_at = UTC_TIMESTAMP(), cancel_reason = %s WHERE appointment_id = %s AND status = 'CONFIRMED'",
            (status, reason, appt["appointment_id"]),
        )
        if self.cur.rowcount != 1:
            raise ApiError(409, "ALREADY_CANCELLED", "This appointment is no longer booked.")
        self.cur.execute(
            "INSERT INTO refund_ledger (appointment_id, patient_id, fee, amount, refund_percent, tier, reason) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (appt["appointment_id"], appt["patient_id"], appt["fee"], amount, percent, tier, reason),
        )

    def get_doctor_slot(self, slot_id, doctor_id):
        self.cur.execute(
            "SELECT slot_id, doctor_id, slot_start, status FROM appointment_slots "
            "WHERE slot_id = %s AND doctor_id = %s FOR UPDATE", (slot_id, doctor_id),
        )
        return self.cur.fetchone()

    def booked_for_slot(self, slot_id):
        self.cur.execute(APPT_SELECT + "WHERE a.slot_id = %s AND a.status = 'CONFIRMED' FOR UPDATE", (slot_id,))
        return self.cur.fetchall()

    def refunds(self, patient_id):
        self.cur.execute(
            "SELECT r.refund_id, r.appointment_id, r.fee, r.amount, r.refund_percent, r.tier, r.reason, "
            "r.created_at, a.slot_start, c.name AS consultation_type, dp.full_name AS doctor_name "
            "FROM refund_ledger r JOIN appointments a ON a.appointment_id = r.appointment_id "
            "JOIN consultation_catalogue c ON c.consultation_type_id = a.consultation_type_id "
            "LEFT JOIN doctor_profiles dp ON dp.user_id = a.doctor_id "
            "WHERE r.patient_id = %s ORDER BY r.refund_id DESC LIMIT 200", (patient_id,),
        )
        return self.cur.fetchall()


# --------------------------------------------------------------------------- #
# Serialisers
# --------------------------------------------------------------------------- #

def appointment_view(a, role="PATIENT"):
    out = {
        "appointment_id": int(a["appointment_id"]),
        "slot_id": int(a["slot_id"]),
        "doctor_id": int(a["doctor_id"]),
        "doctor_name": a.get("doctor_name"),
        "consultation_type": a.get("consultation_type"),
        "slot_start": to_ist(a["slot_start"]),
        "slot_end": to_ist(a["slot_end"]),
        "fee": money(a["fee"]),
        "status": a["status"],
        "cancel_reason": a.get("cancel_reason"),
        "refund": None,
    }
    if role == "DOCTOR":
        out["patient_name"] = a.get("patient_name")
    if a.get("refund_tier"):
        out["refund"] = {
            "tier": a["refund_tier"],
            "percent": int(a["refund_percent"]),
            "amount": money(a["refund_amount"]),
        }
    return out


# --------------------------------------------------------------------------- #
# The engine (stores are injected, so tests run without AWS)
# --------------------------------------------------------------------------- #

class Engine:
    def __init__(self, slots, idem, events, metrics, sleep=time.sleep, clock=utcnow):
        self.slots, self.idem, self.events, self.metrics = slots, idem, events, metrics
        self.sleep, self.clock = sleep, clock

    def _backoff(self, attempt):
        self.sleep(random.uniform(0, min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))))

    # -- seats -------------------------------------------------------------- #
    def reserve_seat(self, slot):
        """Optimistic loop. Returns (record, retries). Capacity is enforced by
        the conditional write itself, so it can never be exceeded."""
        sid = slot["slot_id"]
        if self.slots.get(sid) is None:
            self.slots.create(slot)
        attempts = max(MIN_ATTEMPTS, int(slot["capacity"]) + 2)
        retries = 0
        try:
            for attempt in range(attempts):
                item = self.slots.get(sid)
                if item["status"] != "OPEN":
                    raise ApiError(409, "SLOT_UNAVAILABLE", "This slot is no longer available.")
                if item["booked_count"] >= item["capacity"]:
                    self.metrics.count("BookingsRejectedFull", 1)
                    raise ApiError(409, "SLOT_FULL", "This slot is full.")
                try:
                    return self.slots.reserve(sid, item["version"]), retries
                except VersionConflict:
                    retries += 1
                    self._backoff(attempt)
            raise ApiError(409, "SLOT_BUSY", "Too many people are booking this slot right now. Please try again.")
        finally:
            if retries:
                self.metrics.count("BookingConflictRetries", retries)

    def release_seat(self, slot_id):
        """Free one seat (compensation or cancellation). Returns the new record."""
        for attempt in range(8):
            item = self.slots.get(slot_id)
            if not item or item.get("status") != "OPEN" or item["booked_count"] <= 0:
                return item
            try:
                return self.slots.release(slot_id, item["version"])
            except VersionConflict:
                self._backoff(attempt)
        self.metrics.count("SeatReleaseFailures", 1)
        logger.error("Could not release seat for slot %s", slot_id)
        return None

    # -- booking ------------------------------------------------------------ #
    def book(self, repo, user_id, slot_id, client_key, request_hash):
        patient_id = repo.patient_id(user_id)
        if patient_id is None:
            raise ApiError(409, "PROFILE_REQUIRED", "Complete your patient profile before booking.")

        key = f"{user_id}:{client_key}"
        existing = self.idem.begin(key, request_hash)
        if existing is not None:
            if existing.get("request_hash") != request_hash:
                raise ApiError(422, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used with a different request.")
            if existing.get("state") == "COMPLETED":
                result = json.loads(existing["result"])
                return 200, {**result, "idempotent_replay": True}
            raise ApiError(409, "REQUEST_IN_PROGRESS", "The original request is still being processed.")

        prior = repo.find_by_key(patient_id, client_key)
        if prior:  # RDS already holds this key (e.g. the idempotency record was lost)
            result = {"appointment": appointment_view(prior)}
            self.idem.complete(key, result)
            return 200, {**result, "idempotent_replay": True}

        seat = None
        try:
            slot = repo.get_slot(slot_id)
            now = self.clock()
            if not slot:
                raise ApiError(404, "SLOT_NOT_FOUND", "Slot not found.")
            if slot["slot_start"] <= now:
                raise ApiError(422, "SLOT_IN_PAST", "This slot has already started.")
            if slot["status"] not in ("AVAILABLE", "BOOKED"):
                raise ApiError(409, "SLOT_UNAVAILABLE", "This slot is not open for booking.")
            if slot.get("fee") is None:
                raise ApiError(409, "TYPE_NOT_OFFERED", "The doctor no longer offers this consultation type.")
            enforce_limits(repo.upcoming(patient_id, now), slot, limit_config())

            seat, _ = self.reserve_seat(slot)
            appt, created = repo.insert_appointment(patient_id, slot, client_key)
            if not created:  # RDS already had this key: give the seat back
                self.release_seat(slot_id)
                seat = None
            elif seat["booked_count"] >= seat["capacity"]:
                repo.set_slot_status(slot_id, "BOOKED", "AVAILABLE")
            repo.commit()
        except BaseException:
            try:
                repo.rollback()
            except Exception:
                pass
            if seat is not None:
                self.release_seat(slot_id)
            self.idem.abandon(key)
            raise

        result = {"appointment": appointment_view(appt)}
        self.idem.complete(key, result)
        if created:
            self.metrics.count("AppointmentsBooked", 1)
            self.events.publish("AppointmentBooked", {
                "appointment_id": int(appt["appointment_id"]), "slot_id": int(slot_id),
                "doctor_id": int(slot["doctor_id"]), "patient_id": patient_id,
                "consultation_type_id": int(slot["consultation_type_id"]),
                "slot_start": slot["slot_start"].isoformat() + "Z", "fee": money(slot["fee"]),
                "seats_free": int(seat["capacity"]) - int(seat["booked_count"]),
            })
        return (201 if created else 200), {**result, "idempotent_replay": not created}

    # -- cancellation ------------------------------------------------------- #
    def _after_cancel(self, appt, status, tier, percent, amount, reason):
        item = self.release_seat(int(appt["slot_id"]))
        self.events.publish("AppointmentCancelled", {
            "appointment_id": int(appt["appointment_id"]), "slot_id": int(appt["slot_id"]),
            "doctor_id": int(appt["doctor_id"]), "patient_id": int(appt["patient_id"]),
            "status": status, "tier": tier, "refund_percent": percent,
            "refund_amount": money(amount), "reason": reason,
        })
        if item:
            self.events.publish("SlotSeatFreed", {
                "slot_id": int(appt["slot_id"]), "doctor_id": int(appt["doctor_id"]),
                "seats_free": int(item["capacity"]) - int(item["booked_count"]),
                "capacity": int(item["capacity"]),
            })

    def cancel_by_patient(self, repo, user_id, appointment_id, reason=None):
        patient_id = repo.patient_id(user_id)
        appt = repo.get_appointment(appointment_id, patient_id=patient_id, for_update=True) if patient_id else None
        if not appt:
            raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Appointment not found.")
        if appt["status"] != "CONFIRMED":
            return 200, {"appointment": appointment_view(appt), "already_cancelled": True}
        now = self.clock()
        if appt["slot_start"] <= now:
            raise ApiError(409, "TOO_LATE", "This appointment has already started.")
        tier, percent = refund_tier(appt["slot_start"], now, refund_config())
        amount = refund_amount(appt["fee"], percent)
        repo.cancel(appt, "CANCELLED_BY_PATIENT", reason or "Cancelled by patient", tier, percent, amount)
        repo.set_slot_status(int(appt["slot_id"]), "AVAILABLE", "BOOKED")
        repo.commit()
        self.metrics.count("CancellationsByTier", 1, Tier=tier)
        self.metrics.count("RefundAmountTotal", float(amount), unit="None")
        self._after_cancel(appt, "CANCELLED_BY_PATIENT", tier, percent, amount, reason or "Cancelled by patient")
        return 200, {"appointment": appointment_view(repo.get_appointment(appointment_id), "PATIENT"), "already_cancelled": False}

    def cancel_slot_by_doctor(self, repo, doctor_id, slot_id, reason=None):
        slot = repo.get_doctor_slot(slot_id, doctor_id)
        if not slot:
            raise ApiError(404, "SLOT_NOT_FOUND", "Slot not found.")
        if slot["status"] not in ("AVAILABLE", "BOOKED"):
            raise ApiError(409, "SLOT_NOT_CANCELLABLE", "Only published slots can be cancelled.")
        if slot["slot_start"] <= self.clock():
            raise ApiError(409, "TOO_LATE", "This slot has already started.")
        text = reason or "Cancelled by doctor"
        affected = repo.booked_for_slot(slot_id)
        for appt in affected:
            repo.cancel(appt, "CANCELLED_BY_DOCTOR", text, "DOCTOR_CANCELLED", 100, refund_amount(appt["fee"], 100))
        repo.set_slot_status(slot_id, "BLOCKED", slot["status"])
        repo.commit()
        self.slots.close(slot_id)  # in-flight reservations lose their conditional write
        if affected:
            self.metrics.count("CancellationsByTier", len(affected), Tier="DOCTOR_CANCELLED")
            self.metrics.count("RefundAmountTotal", float(sum(refund_amount(a["fee"], 100) for a in affected)), unit="None")
        for appt in affected:
            amount = refund_amount(appt["fee"], 100)
            self.events.publish("AppointmentCancelled", {
                "appointment_id": int(appt["appointment_id"]), "slot_id": int(slot_id),
                "doctor_id": int(doctor_id), "patient_id": int(appt["patient_id"]),
                "status": "CANCELLED_BY_DOCTOR", "tier": "DOCTOR_CANCELLED",
                "refund_percent": 100, "refund_amount": money(amount), "reason": text,
            })
            self.events.publish("SlotSeatFreed", {"slot_id": int(slot_id), "doctor_id": int(doctor_id), "seats_free": 0, "capacity": 0, "closed": True})
        return 200, {"slot_id": int(slot_id), "cancelled_appointments": len(affected), "refund_percent": 100}


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = Engine(SlotStore(), IdempotencyStore(), Events(), Metrics())
    return _engine


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

def _header(event, name):
    for k, v in (event.get("headers") or {}).items():
        if k.lower() == name.lower():
            return v
    return None


def _int(value, label):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "BAD_REQUEST", f"{label} must be an integer.")


def route(event, repo, engine):
    ctx = (event.get("requestContext") or {}).get("authorizer") or {}
    role, user_id = ctx.get("role"), _int(ctx.get("userId"), "user id")
    method, resource = event.get("httpMethod"), event.get("resource")
    path = event.get("pathParameters") or {}
    payload = read_body(event)
    if payload is None:
        raise ApiError(400, "BAD_JSON", "Request body must be valid JSON.")

    def need(expected):
        if role != expected:
            raise ApiError(403, "WRONG_ROLE", f"This area is for {expected.lower()}s.")

    if (method, resource) == ("POST", "/appointments"):
        need("PATIENT")
        slot_id = _int(payload.get("slot_id"), "slot_id")
        key = _header(event, "Idempotency-Key") or payload.get("idempotency_key")
        if not key or not KEY_RE.match(str(key)):
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key of 8-128 letters, digits or . _ : -")
        return engine.book(repo, user_id, slot_id, str(key), f"slot:{slot_id}")

    if (method, resource) in (("GET", "/appointments"), ("GET", "/doctor/appointments")):
        if resource == "/doctor/appointments":
            need("DOCTOR")
        if role == "DOCTOR":
            rows = repo.list_appointments(doctor_id=user_id)
        else:
            pid = repo.patient_id(user_id)
            rows = repo.list_appointments(patient_id=pid) if pid else []
        return 200, {"items": [appointment_view(r, role) for r in rows]}

    if (method, resource) == ("GET", "/appointments/{appointment_id}"):
        aid = _int(path.get("appointment_id"), "appointment_id")
        if role == "DOCTOR":
            appt = repo.get_appointment(aid, doctor_id=user_id)
        else:
            pid = repo.patient_id(user_id)
            appt = repo.get_appointment(aid, patient_id=pid) if pid else None
        if not appt:
            raise ApiError(404, "APPOINTMENT_NOT_FOUND", "Appointment not found.")
        return 200, {"appointment": appointment_view(appt, role)}

    if (method, resource) == ("DELETE", "/appointments/{appointment_id}"):
        need("PATIENT")
        return engine.cancel_by_patient(repo, user_id, _int(path.get("appointment_id"), "appointment_id"), payload.get("reason"))

    if (method, resource) == ("GET", "/patient/refunds"):
        need("PATIENT")
        pid = repo.patient_id(user_id)
        rows = repo.refunds(pid) if pid else []
        items = [{
            "refund_id": int(r["refund_id"]), "appointment_id": int(r["appointment_id"]),
            "doctor_name": r.get("doctor_name"), "consultation_type": r.get("consultation_type"),
            "slot_start": to_ist(r["slot_start"]), "fee": money(r["fee"]), "amount": money(r["amount"]),
            "percent": int(r["refund_percent"]), "tier": r["tier"], "reason": r.get("reason"),
            "created_at": r["created_at"].replace(tzinfo=timezone.utc).isoformat() if r.get("created_at") else None,
        } for r in rows]
        total = sum((Decimal(i["amount"]) for i in items), Decimal("0.00"))
        return 200, {"items": items, "total_refunded": money(total)}

    if (method, resource) == ("POST", "/doctor/slots/{slot_id}/cancel"):
        need("DOCTOR")
        return engine.cancel_slot_by_doctor(repo, user_id, _int(path.get("slot_id"), "slot_id"), payload.get("reason"))

    raise ApiError(404, "NOT_FOUND", "Route not found.")


def handler(event, context):
    cid = correlation(event)
    conn = None
    try:
        conn = dbconn(config())
        engine = get_engine()
        engine.events.cid = cid
        status, result = route(event, Repo(conn), engine)
        return response(status, {**result, "correlationId": cid})
    except ApiError as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return response(exc.status, {"message": exc.message, "code": exc.code, "correlationId": cid})
    except Exception:
        logger.exception("Appointments request failed (%s)", cid)
        return response(500, {"message": "Appointment request failed", "correlationId": cid})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
