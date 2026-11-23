"""Milestone 6 unit tests: notification matrix, exactly-once dispatch, reminders and no-show sweeper."""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from fakes import load_lambda

nt = load_lambda("notifications")
jobs = load_lambda("scheduled_jobs")


class FakeLog:
    def __init__(self): self.keys = set()
    def put_item(self, Item, ConditionExpression=None):
        if Item["dispatch_key"] in self.keys:
            raise nt.ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.keys.add(Item["dispatch_key"])
    def delete_item(self, Key): self.keys.discard(Key["dispatch_key"])


def wire():
    log, sns = FakeLog(), MagicMock()
    nt.dispatch_log, nt.sns = (lambda: log), (lambda: sns)
    nt.os.environ.update(PATIENT_TOPIC_ARN="arn:p", DOCTOR_TOPIC_ARN="arn:d")
    return log, sns


def sqs(dt, detail, eid="evt-1"):
    return {"Records": [{"messageId": "m1", "body": json.dumps({"id": eid, "detail-type": dt, "detail": detail})}]}


def test_recipient_matrix():
    r = nt.recipients
    assert [x[0] for x in r("AppointmentBooked", {"patient_id": 1, "doctor_id": 2})] == ["patient", "doctor"]
    assert [x[0] for x in r("AppointmentRescheduled", {"patient_id": 1, "doctor_id": 2})] == ["patient", "doctor"]
    assert [x[0] for x in r("AppointmentCancelled", {"status": "CANCELLED_BY_PATIENT", "patient_id": 1, "doctor_id": 2})] == ["patient", "doctor"]
    assert [x[0] for x in r("AppointmentCancelled", {"status": "CANCELLED_BY_DOCTOR", "patient_id": 1, "doctor_id": 2})] == ["patient"]
    for dt in ("WaitlistOfferIssued", "WaitlistOfferExpired", "ReminderDue"):
        assert [x[0] for x in r(dt, {"patient_id": 1, "doctor_id": 2})] == ["patient"]


def test_same_event_delivered_twice_sends_once():
    log, sns = wire()
    ev = sqs("AppointmentBooked", {"appointment_id": 9, "patient_id": 1, "doctor_id": 2})
    nt.handler(ev, None); nt.handler(ev, None)
    assert sns.publish.call_count == 2                     # patient + doctor, once each


def test_sns_failure_is_retried_not_lost():
    log, sns = wire()
    sns.publish.side_effect = [RuntimeError("down"), None]
    ev = sqs("WaitlistOfferIssued", {"offer_id": "1-x", "patient_id": 1})
    assert nt.handler(ev, None)["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert nt.handler(ev, None)["batchItemFailures"] == []
    assert sns.publish.call_count == 2


class Cur:
    def __init__(self, rows): self.rows, self.rowcount, self.sql = rows, 1, []
    def execute(self, sql, params=None): self.sql.append(sql)
    def fetchall(self): return self.rows
    def fetchone(self): return {"ns": 1, "done": 2}


def test_reminders_exactly_once_when_run_twice():
    now = datetime(2026, 11, 20, 10, 0)
    rows = [{"appointment_id": 5, "patient_id": 1, "doctor_id": 2, "slot_start": now + timedelta(hours=2)}]
    log, sent = jobs.DispatchLog.__new__(jobs.DispatchLog), []
    keys = set()
    log.claim = lambda k: (k not in keys) and not keys.add(k)
    log.release = lambda k: keys.discard(k)
    pub = lambda d: sent.append(d)
    assert jobs.run_reminders(Cur(rows), log, now, pub, hours=(2,), catchup=30) == 1
    assert jobs.run_reminders(Cur(rows), log, now, pub, hours=(2,), catchup=30) == 0
    assert len(sent) == 1 and sent[0]["interval"] == "REMINDER_2H"


def test_no_show_marks_stale_confirmed_and_second_run_is_noop():
    class C(Cur):
        def __init__(self): super().__init__([{"appointment_id": 5, "patient_id": 1, "fee": 100}]); self.done = False
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.startswith("UPDATE"):
                self.rowcount = 0 if self.done else 1
                self.done = True
    conn, cur = MagicMock(), C()
    assert jobs.run_no_show(conn, cur, datetime(2026, 11, 20, 12, 0), 30) == 1
    assert jobs.run_no_show(conn, cur, datetime(2026, 11, 20, 12, 0), 30) == 0
