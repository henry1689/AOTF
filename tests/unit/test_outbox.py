"""M0-A3b2 outbox delivery 服务测试。

DB 一律使用 pytest tmp_path；不访问网络/时钟/环境秘密；时间全用显式 at。
投递为 at-least-once：崩溃重投可观测、receiver 按 message_id 去重一次。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aotf import db, outbox, store
from aotf.errors import AotfError, ErrorCode
from aotf.models import OutboxRecord

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
TOPIC = "task.completed"


class _OkSender:
    def __init__(self):
        self.sent = []

    def send(self, message_id, topic, payload):
        self.sent.append(message_id)


class _FlakySender:
    def __init__(self, always=False, fail_ids=frozenset()):
        self.sent = []
        self._always = always
        self._fail = set(fail_ids)

    def send(self, message_id, topic, payload):
        self.sent.append(message_id)
        if self._always or message_id in self._fail:
            raise RuntimeError("boom")


class _DedupReceiver:
    def __init__(self):
        self.applied = []

    def apply(self, message_id):
        if message_id not in self.applied:
            self.applied.append(message_id)


class _RecordingSender:
    def __init__(self, receiver):
        self.sent = []
        self._receiver = receiver

    def send(self, message_id, topic, payload):
        self.sent.append(message_id)
        self._receiver.apply(message_id)


def _db(tmp_path, name="t.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    return conn


def _insert(conn, message_id, created_at=T0, next_attempt_at=None):
    store.insert_outbox(conn, OutboxRecord(
        message_id=message_id, topic=TOPIC, payload_json="{}",
        status="pending", attempts=0, next_attempt_at=next_attempt_at,
        created_at=created_at,
    ))
    conn.commit()


def _run(conn, sender, at, max_attempts=3, base=60):
    report = outbox.process_due(conn, sender=sender, at=at,
                                max_attempts=max_attempts,
                                base_delay_seconds=base)
    conn.commit()
    return report


def test_process_due_delivers_due_pending_only(tmp_path) -> None:
    conn = _db(tmp_path)
    _insert(conn, "msg-1")
    _insert(conn, "msg-2")
    _insert(conn, "msg-future", next_attempt_at=T0 + timedelta(hours=1))
    report = _run(conn, _OkSender(), T0)
    assert report == outbox.DeliveryReport(delivered=2, failed=0, deferred=0)
    assert store.get_outbox(conn, "msg-1").status == "delivered"
    assert store.get_outbox(conn, "msg-2").status == "delivered"
    assert store.get_outbox(conn, "msg-future").status == "pending"
    conn.close()


def test_process_due_skips_future_and_terminal_rows(tmp_path) -> None:
    conn = _db(tmp_path)
    _insert(conn, "msg-future", next_attempt_at=T0 + timedelta(hours=1))
    store.mark_outbox_delivered(conn, message_id="msg-future", attempts=1)
    _insert(conn, "msg-done")
    store.mark_outbox_delivered(conn, message_id="msg-done", attempts=1)
    _insert(conn, "msg-fail")
    store.mark_outbox_failed(conn, message_id="msg-fail", attempts=3)
    conn.commit()
    report = _run(conn, _OkSender(), T0)
    assert report == outbox.DeliveryReport(delivered=0, failed=0, deferred=0)
    assert store.get_outbox(conn, "msg-future").status == "delivered"
    assert store.get_outbox(conn, "msg-done").status == "delivered"
    assert store.get_outbox(conn, "msg-fail").status == "failed"
    conn.close()


def test_process_due_retry_backoff_then_success(tmp_path) -> None:
    conn = _db(tmp_path)
    _insert(conn, "msg-1")
    flaky = _FlakySender(always=True)
    report = _run(conn, flaky, T0)
    assert report == outbox.DeliveryReport(delivered=0, failed=0, deferred=1)
    m = store.get_outbox(conn, "msg-1")
    assert m.status == "pending"
    assert m.attempts == 1
    assert m.next_attempt_at == T0 + timedelta(seconds=60)
    second = _run(conn, _OkSender(), T0 + timedelta(seconds=60))
    assert second == outbox.DeliveryReport(delivered=1, failed=0, deferred=0)
    assert store.get_outbox(conn, "msg-1").attempts == 2
    conn.close()


def test_process_due_fails_after_max_attempts(tmp_path) -> None:
    conn = _db(tmp_path)
    _insert(conn, "msg-1")
    _run(conn, _FlakySender(always=True), T0, max_attempts=2)
    m = store.get_outbox(conn, "msg-1")
    assert m.status == "pending"
    assert m.attempts == 1
    report = _run(conn, _FlakySender(always=True), T0 + timedelta(seconds=60),
                  max_attempts=2)
    assert report == outbox.DeliveryReport(delivered=0, failed=1, deferred=0)
    m = store.get_outbox(conn, "msg-1")
    assert m.status == "failed"
    assert m.attempts == 2
    assert m.next_attempt_at is None
    conn.close()


def test_at_least_once_crash_redelivery_receiver_dedups(tmp_path) -> None:
    conn = _db(tmp_path)
    _insert(conn, "msg-1")
    receiver = _DedupReceiver()
    sender = _RecordingSender(receiver)
    outbox.process_due(conn, sender=sender, at=T0, max_attempts=3,
                       base_delay_seconds=60)
    conn.rollback()
    assert store.get_outbox(conn, "msg-1").status == "pending"
    _run(conn, sender, T0)
    assert sender.sent == ["msg-1", "msg-1"]
    assert receiver.applied == ["msg-1"]
    assert store.get_outbox(conn, "msg-1").status == "delivered"
    conn.close()


def test_process_due_report_totals(tmp_path) -> None:
    conn = _db(tmp_path)
    _insert(conn, "msg-1")
    _insert(conn, "msg-2")
    _insert(conn, "msg-bad")
    report = _run(conn, _FlakySender(fail_ids={"msg-bad"}), T0, max_attempts=1)
    assert report == outbox.DeliveryReport(delivered=2, failed=1, deferred=0)
    assert store.get_outbox(conn, "msg-bad").status == "failed"
    conn.close()


@pytest.mark.parametrize("kwargs", [
    {"max_attempts": 0},
    {"base_delay_seconds": -1},
])
def test_process_due_rejects_invalid_args(tmp_path, kwargs) -> None:
    conn = _db(tmp_path)
    params = {"max_attempts": 3, "base_delay_seconds": 60}
    params.update(kwargs)
    with pytest.raises(AotfError) as exc:
        outbox.process_due(conn, sender=_OkSender(), at=T0, **params)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    conn.close()


def test_process_due_empty_noop(tmp_path) -> None:
    conn = _db(tmp_path)
    report = _run(conn, _OkSender(), T0)
    assert report == outbox.DeliveryReport(delivered=0, failed=0, deferred=0)
    conn.close()


def test_process_due_delivers_in_created_order(tmp_path) -> None:
    conn = _db(tmp_path)
    _insert(conn, "msg-1", created_at=T0)
    _insert(conn, "msg-2", created_at=T0 + timedelta(seconds=1))
    _insert(conn, "msg-3", created_at=T0 + timedelta(seconds=2))
    sender = _OkSender()
    _run(conn, sender, T0)
    assert sender.sent == ["msg-1", "msg-2", "msg-3"]
    conn.close()
