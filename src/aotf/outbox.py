"""AOTF transactional outbox delivery service (A3b2).

按 created_at 序投递到期 pending 消息：成功 mark_outbox_delivered；抛异常则
attempts+1，达 max_attempts 转 failed（health 可见，不静默忽略），未达按指数
退避调度重试。投递为 at-least-once：send 成功后、事务提交前崩溃会重投同一
message_id——重复外部副作用由 receiver 按 message_id 去重吸收，本模块不保证
exactly-once。不 commit（调用方事务）；无时钟/网络；M0-A 由 fake sender 实现，
不发真实 webhook。in_flight/claim 与事务 enqueue 编排不属本模块。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from aotf import store
from aotf.errors import AotfError, ErrorCode


class Sender(Protocol):
    """投递目标抽象；M0-A 由 fake sender 实现，不发真实 webhook。"""

    def send(self, message_id: str, topic: str, payload: str) -> None:
        ...


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    delivered: int
    failed: int
    deferred: int


def _err(code, message, details):
    raise AotfError(code, message, details)


def _valid_at(at):
    if type(at) is not datetime or at.utcoffset() != timedelta(0):
        _err(ErrorCode.INVALID_INPUT, "at must be aware UTC datetime", {"field": "at"})


def process_due(
    conn: sqlite3.Connection,
    *,
    sender: Sender,
    at: datetime,
    max_attempts: int,
    base_delay_seconds: int,
) -> DeliveryReport:
    _valid_at(at)
    if type(max_attempts) is not int or max_attempts < 1:
        _err(ErrorCode.INVALID_INPUT, "max_attempts must be positive int",
             {"field": "max_attempts"})
    if type(base_delay_seconds) is not int or base_delay_seconds < 0:
        _err(ErrorCode.INVALID_INPUT, "base_delay_seconds must be non-negative int",
             {"field": "base_delay_seconds"})
    rows = conn.execute(
        "SELECT message_id, topic, payload_json, attempts FROM outbox "
        "WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY created_at ASC, message_id ASC",
        (at.isoformat(),),
    ).fetchall()
    delivered = 0
    failed = 0
    deferred = 0
    for row in rows:
        message_id = row["message_id"]
        attempts = row["attempts"] + 1
        try:
            sender.send(message_id, row["topic"], row["payload_json"])
        except Exception:
            if attempts >= max_attempts:
                if store.mark_outbox_failed(
                    conn, message_id=message_id, attempts=attempts,
                ):
                    failed += 1
            else:
                next_at = at + timedelta(
                    seconds=base_delay_seconds * (2 ** (attempts - 1)),
                )
                if store.mark_outbox_retry(
                    conn, message_id=message_id, attempts=attempts,
                    next_attempt_at=next_at,
                ):
                    deferred += 1
        else:
            if store.mark_outbox_delivered(
                conn, message_id=message_id, attempts=attempts,
            ):
                delivered += 1
    return DeliveryReport(delivered=delivered, failed=failed, deferred=deferred)
