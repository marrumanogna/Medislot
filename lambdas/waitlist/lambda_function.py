"""MediSlot Milestone 6: waitlist and hold offers.

Entry points (one function, three triggers):
    API Gateway   POST/DELETE /slots/{slot_id}/waitlist, GET /patient/waitlists,
                  POST /waitlist-offers/{offer_id}/accept            (PATIENT)
    SQS           SlotSeatFreed events (EventBridge -> SQS -> here)  -> issue an offer
    Scheduler     {"action": "expire_offers"} every minute           -> expiry sweeper

Seat accounting on the DynamoDB slot record:
    booked_count = seats occupied (confirmed appointments PLUS held seats)
    held_count   = how many of those occupied seats are held for a live offer
So the normal booking condition `booked_count < capacity` already makes a held seat
invisible to everybody else, with no change to the booking engine.

Exclusivity: an offer needs (1) a conditional WAITING->OFFERED claim of the waitlist entry and
(2) a conditional hold `booked_count < capacity`. The hold can only succeed once per free seat, so
two live offers can never exist for the same seat.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from common import body as read_body, config, correlation, dbconn, response

logger = logging.getLogger()
logger.setLevel(logging.INFO)
ENV = os.environ.get("ENVIRONMENT", "dev")

DEFAULTS = {"waitlist/max-per-patient": 5, "waitlist/hold-minutes": 15}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class SeatUnavailable(Exception):
    """A conditional write on the slot record failed (no free seat / nothing held)."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


_cache = {}


def ssm_int(name, default=None):
    default = DEFAULTS.get(name, default)
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        from common import ssm
        value = int(ssm.get_parameter(Name=f"/medislot/{ENV}/{name}")["Parameter"]["Value"])
    except Exception:
        value = default
    _cache[name] = (time.time(), value)
    return value


def _plain(item):
    return {k: (int(v) if isinstance(v, Decimal) else v) for k, v in item.items()} if item else None


def _failed(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _t(suffix):
    from common import table
    return table(f"MediSlot-{ENV}-{suffix}")


class Metrics:
    def count(self, name, value=1, unit="Count"):
        print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": "medislot", "Dimensions": [["Environment"]], "Metrics": [{"Name": name, "Unit": unit}]}]},
            "Environment": ENV, name: value}))


class Events:
    cid = None

    def publish(self, detail_type, detail):
        try:
            import boto3
            if not hasattr(self, "_c"):
                self._c = boto3.client("events")
            if self.cid:
                detail = {**detail, "correlationId": self.cid}
            self._c.put_events(Entries=[{"Source": "medislot.waitlist", "DetailType": detail_type,
                                         "Detail": json.dumps(detail, default=str), "EventBusName": "default"}])
        except Exception:
            logger.exception("Could not publish %s", detail_type)


# --------------------------------------------------------------------------- #
# DynamoDB stores
# --------------------------------------------------------------------------- #

class Seats:
    """Slot-record operations used by the waitlist (counters only; booking lives elsewhere)."""

    @property
    def t(self):
        if not hasattr(self, "_t"):
            self._t = _t("Slots")
        return self._t

    def get(self, slot_id):
        return _plain(self.t.get_item(Key={"slot_id": str(slot_id)}, ConsistentRead=True).get("Item"))

    def _update(self, slot_id, expr, cond, values):
        try:
            out = self.t.update_item(
                Key={"slot_id": str(slot_id)}, UpdateExpression=expr, ConditionExpression=cond,
                ExpressionAttributeNames={"#s": "status", "#cap": "capacity"},
                ExpressionAttributeValues=values, ReturnValues="ALL_NEW")
            return _plain(out["Attributes"])
        except Exception as exc:
            if _failed(exc):
                raise SeatUnavailable()
            raise

    def hold(self, slot_id):
        return self._update(
            slot_id,
            "SET booked_count = booked_count + :one, held_count = if_not_exists(held_count, :z) + :one, version = version + :one",
            "booked_count < #cap AND #s = :open", {":one": 1, ":z": 0, ":open": "OPEN"})

    def convert(self, slot_id):
        """Offer accepted: the held seat becomes a confirmed appointment (occupancy unchanged)."""
        return self._update(
            slot_id, "SET held_count = held_count - :one, version = version + :one",
            "held_count > :z", {":one": 1, ":z": 0})

    def free_hold(self, slot_id):
        """Offer expired or declined: give the seat back."""
        return self._update(
            slot_id,
            "SET booked_count = booked_count - :one, held_count = held_count - :one, version = version + :one",
            "held_count > :z AND booked_count > :z", {":one": 1, ":z": 0})


class Waitlist:
    @property
    def t(self):
        if not hasattr(self, "_t"):
            self._t = _t("Waitlist")
        return self._t

    def join(self, slot_id, patient_id, now, slot_start=None):
        entry = {"slot_id": str(slot_id), "waitlist_entry": f"{now.isoformat(timespec='microseconds')}#{int(patient_id):012d}",
                 "patient_id": str(patient_id), "joined_at": now.isoformat(timespec="microseconds"), "status": "WAITING"}
        if slot_start is not None:
            entry["slot_start_epoch"] = int(slot_start.replace(tzinfo=timezone.utc).timestamp())
        self.t.put_item(Item=entry, ConditionExpression="attribute_not_exists(slot_id)")
        return entry

    def entries(self, slot_id):
        from boto3.dynamodb.conditions import Key
        items, kwargs = [], {"KeyConditionExpression": Key("slot_id").eq(str(slot_id)), "ConsistentRead": True}
        while True:
            out = self.t.query(**kwargs)
            items += out["Items"]
            if "LastEvaluatedKey" not in out:
                return sorted(items, key=lambda e: e["waitlist_entry"])
            kwargs["ExclusiveStartKey"] = out["LastEvaluatedKey"]

    def waiting(self, slot_id):
        return [e for e in self.entries(slot_id) if e["status"] == "WAITING"]

    def by_patient(self, patient_id):
        from boto3.dynamodb.conditions import Key
        out = self.t.query(IndexName="GSI_PatientWaitlists", KeyConditionExpression=Key("patient_id").eq(str(patient_id)))
        return sorted(out["Items"], key=lambda e: e["joined_at"])

    def _cond(self, entry, new, old, extra=""):
        try:
            self.t.update_item(
                Key={"slot_id": entry["slot_id"], "waitlist_entry": entry["waitlist_entry"]},
                UpdateExpression="SET #st = :new" + extra[0] if extra else "SET #st = :new",
                ConditionExpression="#st = :old", ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":new": new, ":old": old, **(extra[1] if extra else {})})
            return True
        except Exception as exc:
            if _failed(exc):
                return False
            raise

    def claim(self, entry, offer_id):
        return self._cond(entry, "OFFERED", "WAITING", (", offer_id = :o", {":o": offer_id}))

    def revert(self, entry):
        return self._cond(entry, "WAITING", "OFFERED")

    def remove(self, entry):
        self.t.delete_item(Key={"slot_id": entry["slot_id"], "waitlist_entry": entry["waitlist_entry"]})


class Offers:
    @property
    def t(self):
        if not hasattr(self, "_t"):
            self._t = _t("HoldOffers")
        return self._t

    def create(self, offer):
        self.t.put_item(Item=offer)

    def get(self, offer_id):
        slot_id = offer_id.split("-", 1)[0]
        return _plain(self.t.get_item(Key={"slot_id": slot_id, "offer_id": offer_id}, ConsistentRead=True).get("Item"))

    def transition(self, offer, new, old="LIVE"):
        try:
            self.t.update_item(
                Key={"slot_id": offer["slot_id"], "offer_id": offer["offer_id"]},
                UpdateExpression="SET #st = :new", ConditionExpression="#st = :old",
                ExpressionAttributeNames={"#st": "status"}, ExpressionAttributeValues={":new": new, ":old": old})
            return True
        except Exception as exc:
            if _failed(exc):
                return False
            raise

    def due(self, now_epoch, limit=50):
        from boto3.dynamodb.conditions import Key
        out = self.t.query(IndexName="GSI_OfferExpiry", Limit=limit,
                           KeyConditionExpression=Key("status").eq("LIVE") & Key("expires_at").lte(now_epoch))
        return [_plain(i) for i in out["Items"]]

    def live_for_slot_patient(self, slot_id, patient_id):
        from boto3.dynamodb.conditions import Key
        out = self.t.query(KeyConditionExpression=Key("slot_id").eq(str(slot_id)), ConsistentRead=True)
        return [_plain(i) for i in out["Items"] if i["status"] == "LIVE" and i["patient_id"] == str(patient_id)]


# --------------------------------------------------------------------------- #
# RDS (small subset; appointments stay the system of record)
# --------------------------------------------------------------------------- #

class Repo:
    def __init__(self, conn):
        self.conn, self.cur = conn, conn.cursor()

    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()

    def patient_id(self, user_id):
        self.cur.execute("SELECT patient_id FROM patients WHERE user_id = %s", (user_id,))
        row = self.cur.fetchone()
        return int(row["patient_id"]) if row else None

    def get_slot(self, slot_id):
        self.cur.execute(
            "SELECT s.slot_id, s.doctor_id, s.consultation_type_id, s.slot_start, s.slot_end, s.status, "
            "c.name AS consultation_type, f.fee, dp.full_name AS doctor_name "
            "FROM appointment_slots s JOIN consultation_catalogue c ON c.consultation_type_id = s.consultation_type_id "
            "LEFT JOIN doctor_consultation_fees f ON f.doctor_id = s.doctor_id AND f.consultation_type_id = s.consultation_type_id AND f.is_active = 1 "
            "LEFT JOIN doctor_profiles dp ON dp.user_id = s.doctor_id WHERE s.slot_id = %s", (slot_id,))
        return self.cur.fetchone()

    def upcoming(self, patient_id, now):
        self.cur.execute("SELECT slot_id, doctor_id FROM appointments WHERE patient_id = %s AND status = 'CONFIRMED' AND slot_start > %s",
                         (patient_id, now))
        return self.cur.fetchall()

    def insert_appointment(self, patient_id, slot, key):
        self.cur.execute(
            "INSERT INTO appointments (slot_id, doctor_id, patient_id, consultation_type_id, slot_start, slot_end, fee, "
            "status, active_slot_id, idempotency_key) VALUES (%s,%s,%s,%s,%s,%s,%s,'CONFIRMED',%s,%s)",
            (slot["slot_id"], slot["doctor_id"], patient_id, slot["consultation_type_id"], slot["slot_start"],
             slot["slot_end"], slot["fee"], slot["slot_id"], key))
        return int(self.cur.lastrowid)

    def find_by_key(self, patient_id, key):
        self.cur.execute("SELECT appointment_id FROM appointments WHERE patient_id = %s AND idempotency_key = %s", (patient_id, key))
        row = self.cur.fetchone()
        return int(row["appointment_id"]) if row else None

    def set_slot_status(self, slot_id, new, old):
        self.cur.execute("UPDATE appointment_slots SET status = %s WHERE slot_id = %s AND status = %s", (new, slot_id, old))


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class WaitlistEngine:
    def __init__(self, wl, offers, seats, events, metrics, clock=utcnow, hold_seconds=None, max_lists=None):
        self.wl, self.offers, self.seats, self.events, self.metrics, self.clock = wl, offers, seats, events, metrics, clock
        self.hold_seconds = hold_seconds or (lambda: ssm_int("waitlist/hold-minutes") * 60)
        self.max_lists = max_lists or (lambda: ssm_int("waitlist/max-per-patient"))

    # -- join / leave ------------------------------------------------------- #
    def join(self, repo, user_id, slot_id):
        pid = repo.patient_id(user_id)
        if pid is None:
            raise ApiError(409, "PROFILE_REQUIRED", "Complete your patient profile first.")
        slot = repo.get_slot(slot_id)
        if not slot:
            raise ApiError(404, "SLOT_NOT_FOUND", "Slot not found.")
        now = self.clock()
        if slot["slot_start"] <= now:
            raise ApiError(422, "SLOT_IN_PAST", "This slot has already started.")
        item = self.seats.get(slot_id)
        if not item or item.get("status") != "OPEN" or item["booked_count"] < item["capacity"]:
            raise ApiError(409, "SLOT_NOT_FULL", "This slot still has seats; book it directly.")
        if any(int(a["slot_id"]) == int(slot_id) for a in repo.upcoming(pid, now)):
            raise ApiError(409, "ALREADY_BOOKED", "You already hold a seat in this slot.")
        now_epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
        mine = [e for e in self.wl.by_patient(pid) if int(e.get("slot_start_epoch", now_epoch + 1)) > now_epoch]
        if any(e["slot_id"] == str(slot_id) for e in mine):
            raise ApiError(409, "ALREADY_WAITLISTED", "You are already on this waitlist.")
        if len(mine) >= self.max_lists():
            raise ApiError(422, "WAITLIST_LIMIT", f"You can be on at most {self.max_lists()} waitlists.")
        entry = self.wl.join(slot_id, pid, now, slot["slot_start"])
        return 201, {"slot_id": int(slot_id), "status": "WAITING", "position": len(self.wl.waiting(slot_id)),
                     "joined_at": entry["joined_at"]}

    def leave(self, repo, user_id, slot_id):
        pid = repo.patient_id(user_id)
        entry = next((e for e in (self.wl.by_patient(pid) if pid else []) if e["slot_id"] == str(slot_id)), None)
        if not entry:
            raise ApiError(404, "NOT_ON_WAITLIST", "You are not on this waitlist.")
        if entry["status"] == "OFFERED":
            offer = self.offers.get(entry["offer_id"])
            if offer and self.offers.transition(offer, "DECLINED"):
                self._free_and_pass_on(offer["slot_id"])
        self.wl.remove(entry)
        return 200, {"slot_id": int(slot_id), "left": True}

    # -- promotion ---------------------------------------------------------- #
    def promote(self, slot_id):
        """Offer ONE free seat to the first waiting patient. Returns the offer or None."""
        item = self.seats.get(slot_id)
        if not item or item.get("status") != "OPEN" or item["booked_count"] >= item["capacity"]:
            return None
        for entry in self.wl.waiting(slot_id):
            offer_id = f"{slot_id}-{uuid.uuid4().hex}"
            if not self.wl.claim(entry, offer_id):
                continue  # another consumer took this patient; try the next one
            try:
                self.seats.hold(slot_id)
            except SeatUnavailable:
                self.wl.revert(entry)  # seat already gone: nobody is offered anything
                return None
            try:
                now = self.clock()
                expires = int(now.replace(tzinfo=timezone.utc).timestamp()) + self.hold_seconds()
                offer = {"slot_id": str(slot_id), "offer_id": offer_id, "patient_id": entry["patient_id"],
                         "waitlist_entry": entry["waitlist_entry"], "status": "LIVE", "expires_at": expires,
                         "created_at": now.isoformat()}
                self.offers.create(offer)
            except Exception:
                try:
                    self.seats.free_hold(slot_id)
                finally:
                    self.wl.revert(entry)
                raise
            self.metrics.count("WaitlistPromotions", 1)
            self.events.publish("WaitlistOfferIssued", {
                "offer_id": offer_id, "slot_id": int(slot_id), "patient_id": int(entry["patient_id"]),
                "expires_at": expires, "slot_start": item.get("start_at")})
            return offer
        return None

    def _free_and_pass_on(self, slot_id):
        try:
            self.seats.free_hold(slot_id)
        except SeatUnavailable:
            return None  # slot closed or already released
        return self.promote(slot_id)

    # -- accept ------------------------------------------------------------- #
    def accept(self, repo, user_id, offer_id):
        pid = repo.patient_id(user_id)
        offer = self.offers.get(offer_id) if offer_id and "-" in offer_id else None
        if not offer or pid is None or offer["patient_id"] != str(pid):
            raise ApiError(404, "OFFER_NOT_FOUND", "Offer not found.")
        key = f"waitlist-{offer_id}"
        if offer["status"] == "ACCEPTED":  # replay
            return 200, {"appointment_id": repo.find_by_key(pid, key), "idempotent_replay": True}
        now = self.clock()
        if offer["status"] != "LIVE" or offer["expires_at"] <= int(now.replace(tzinfo=timezone.utc).timestamp()):
            raise ApiError(410, "OFFER_EXPIRED", "This offer is no longer available.")
        slot = repo.get_slot(int(offer["slot_id"]))
        problem = None
        if not slot or slot.get("fee") is None or slot["status"] not in ("AVAILABLE", "BOOKED"):
            problem = ApiError(409, "SLOT_UNAVAILABLE", "This slot is no longer available.")
        elif any(int(a["slot_id"]) == int(slot["slot_id"]) for a in repo.upcoming(pid, now)):
            problem = ApiError(409, "ALREADY_BOOKED", "You already hold a seat in this slot.")
        else:
            same = sum(1 for a in repo.upcoming(pid, now) if int(a["doctor_id"]) == int(slot["doctor_id"]))
            limit = ssm_int("booking/max-upcoming-per-doctor", 3)
            if same >= limit:
                problem = ApiError(422, "LIMIT_PER_DOCTOR", f"You can hold at most {limit} upcoming appointments with the same doctor.")
        if problem:
            if self.offers.transition(offer, "DECLINED"):
                self._drop_entry(offer)
                self._free_and_pass_on(offer["slot_id"])
            raise problem
        if not self.offers.transition(offer, "ACCEPTED"):
            raise ApiError(409, "OFFER_NOT_AVAILABLE", "This offer was already used or has expired.")
        try:
            appt_id = repo.insert_appointment(pid, slot, key)
            item = self.seats.get(slot["slot_id"])
            if item and item["booked_count"] >= item["capacity"]:
                repo.set_slot_status(slot["slot_id"], "BOOKED", "AVAILABLE")
            repo.commit()
        except BaseException:
            repo.rollback()
            self.offers.transition(offer, "LIVE", "ACCEPTED")  # still live; the patient may retry
            raise
        try:
            self.seats.convert(slot["slot_id"])
        except SeatUnavailable:
            logger.error("held_count already zero for slot %s", slot["slot_id"])
        self._drop_entry(offer)
        self.metrics.count("AppointmentsBooked", 1)
        self.events.publish("AppointmentBooked", {
            "appointment_id": appt_id, "slot_id": int(slot["slot_id"]), "doctor_id": int(slot["doctor_id"]),
            "patient_id": pid, "consultation_type_id": int(slot["consultation_type_id"]),
            "slot_start": slot["slot_start"].isoformat() + "Z", "fee": str(slot["fee"]), "via": "waitlist"})
        return 201, {"appointment_id": appt_id, "slot_id": int(slot["slot_id"]), "idempotent_replay": False}

    def _drop_entry(self, offer):
        try:
            self.wl.remove({"slot_id": offer["slot_id"], "waitlist_entry": offer["waitlist_entry"]})
        except Exception:
            logger.exception("Could not remove waitlist entry")

    def close_slot(self, slot_id):
        """Doctor cancelled the slot: end every offer and clear the queue so nobody is stuck on it."""
        for e in self.wl.entries(slot_id):
            if e["status"] == "OFFERED":
                offer = self.offers.get(e.get("offer_id", ""))
                if offer and self.offers.transition(offer, "EXPIRED"):
                    self.events.publish("WaitlistOfferExpired", {
                        "offer_id": offer["offer_id"], "slot_id": int(slot_id), "patient_id": int(offer["patient_id"])})
            self.wl.remove(e)

    # -- sweeper ------------------------------------------------------------ #
    def expire_offers(self):
        """Safe to run twice: only the run that wins LIVE->EXPIRED frees the seat."""
        now_epoch = int(self.clock().replace(tzinfo=timezone.utc).timestamp())
        expired = 0
        for offer in self.offers.due(now_epoch):
            if not self.offers.transition(offer, "EXPIRED"):
                continue
            expired += 1
            self.metrics.count("OffersExpired", 1)
            self._drop_entry(offer)  # the patient who let the offer lapse leaves the queue
            self.events.publish("WaitlistOfferExpired", {
                "offer_id": offer["offer_id"], "slot_id": int(offer["slot_id"]), "patient_id": int(offer["patient_id"])})
            self._free_and_pass_on(offer["slot_id"])
        return expired

    # -- listing ------------------------------------------------------------ #
    def listing(self, repo, user_id):
        pid = repo.patient_id(user_id)
        items = []
        now_epoch = int(self.clock().replace(tzinfo=timezone.utc).timestamp())
        for e in (self.wl.by_patient(pid) if pid else []):
            if int(e.get("slot_start_epoch", now_epoch + 1)) <= now_epoch:
                continue
            slot = repo.get_slot(int(e["slot_id"])) or {}
            row = {"slot_id": int(e["slot_id"]), "status": e["status"], "joined_at": e["joined_at"],
                   "doctor_name": slot.get("doctor_name"), "consultation_type": slot.get("consultation_type"),
                   "slot_start": slot["slot_start"].isoformat() + "Z" if slot.get("slot_start") else None,
                   "position": None, "offer": None}
            if e["status"] == "WAITING":
                row["position"] = [w["patient_id"] for w in self.wl.waiting(e["slot_id"])].index(e["patient_id"]) + 1
            elif e["status"] == "OFFERED":
                o = self.offers.get(e.get("offer_id", ""))
                if o and o["status"] == "LIVE":
                    row["offer"] = {"offer_id": o["offer_id"], "expires_at": o["expires_at"]}
            items.append(row)
        return 200, {"items": items}


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = WaitlistEngine(Waitlist(), Offers(), Seats(), Events(), Metrics())
    return _engine


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def _int(value, label):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "BAD_REQUEST", f"{label} must be an integer.")


def route(event, repo, engine):
    ctx = (event.get("requestContext") or {}).get("authorizer") or {}
    if ctx.get("role") != "PATIENT":
        raise ApiError(403, "WRONG_ROLE", "This area is for patients.")
    user_id = _int(ctx.get("userId"), "user id")  # acting user comes ONLY from the authorizer
    method, resource, path = event.get("httpMethod"), event.get("resource"), event.get("pathParameters") or {}
    if (method, resource) == ("POST", "/slots/{slot_id}/waitlist"):
        return engine.join(repo, user_id, _int(path.get("slot_id"), "slot_id"))
    if (method, resource) == ("DELETE", "/slots/{slot_id}/waitlist"):
        return engine.leave(repo, user_id, _int(path.get("slot_id"), "slot_id"))
    if (method, resource) == ("GET", "/patient/waitlists"):
        return engine.listing(repo, user_id)
    if (method, resource) == ("POST", "/waitlist-offers/{offer_id}/accept"):
        return engine.accept(repo, user_id, path.get("offer_id"))
    raise ApiError(404, "NOT_FOUND", "Route not found.")


def handle_sqs(event, engine):
    failures = []
    for rec in event["Records"]:
        try:
            msg = json.loads(rec["body"])
            detail = msg.get("detail") or {}
            if msg.get("detail-type") == "SlotSeatFreed":
                engine.events.cid = detail.get("correlationId")
                if detail.get("closed"):
                    engine.close_slot(int(detail["slot_id"]))
                else:
                    engine.promote(int(detail["slot_id"]))
        except Exception:
            logger.exception("Seat-freed handling failed")
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}


def handler(event, context):
    engine = get_engine()
    if "Records" in event:
        return handle_sqs(event, engine)
    if event.get("action") == "expire_offers":
        return {"expired": engine.expire_offers()}
    cid = correlation(event)
    engine.events.cid = cid
    conn = None
    try:
        if read_body(event) is None:
            raise ApiError(400, "BAD_JSON", "Request body must be valid JSON.")
        conn = dbconn(config())
        status, result = route(event, Repo(conn), engine)
        return response(status, {**result, "correlationId": cid})
    except ApiError as exc:
        return response(exc.status, {"message": exc.message, "code": exc.code, "correlationId": cid})
    except Exception:
        logger.exception("Waitlist request failed (%s)", cid)
        return response(500, {"message": "Waitlist request failed", "correlationId": cid})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
